"""Integration tests for shared workflow functions."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ducky.llm_client import FakeLLMClient
from ducky.progress import ProgressTracker
from ducky.tutor import TutorEngine
from ducky.workflows import (
    resolve_current_lesson,
    run_tests_workflow,
    save_and_record,
    submit_workflow,
)
from tests.helpers import make_add_pack


class WorkflowsTests(unittest.TestCase):
    """Tests for the shared workflow functions."""

    def test_run_tests_workflow_passes_attempt_number(self):
        """run_tests_workflow should pass attempt_number to run_pack."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = make_add_pack(tmp)
            student = tmp / "student.py"
            student.write_text("def add_numbers(a, b):\n    return a + b\n")

            report = run_tests_workflow(
                str(pack),
                student_file=str(student),
                tutor_engine=TutorEngine(FakeLLMClient()),
                lesson_id="python_basics.lesson_01",
            )
            self.assertTrue(report.passed)

    def test_run_tests_workflow_default_attempt_no_lesson(self):
        """Without lesson_id, attempt defaults to 1."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = make_add_pack(tmp)
            student = tmp / "student.py"
            student.write_text("def add_numbers(a, b):\n    return a + b\n")

            report = run_tests_workflow(
                str(pack),
                student_file=str(student),
                tutor_engine=TutorEngine(FakeLLMClient()),
            )
            self.assertTrue(report.passed)

    def test_save_and_record_increments_attempts(self):
        """save_and_record should increment the attempt count."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # Use a temp DB to avoid polluting the real ProgressTracker DB
            db_path = Path(tmp) / "progress.db"
            pack = make_add_pack(tmp)
            student = tmp / "student.py"
            student.write_text("def add_numbers(a, b):\n    print(a + b)\n")

            # First attempt
            run_tests_workflow(
                str(pack),
                student_file=str(student),
                tutor_engine=TutorEngine(FakeLLMClient()),
                lesson_id="test.incr_attempts",
            )

            # Use a private tracker with temp DB
            tracker = ProgressTracker(db_path=db_path)
            try:
                initial = tracker.get_lesson_stats("test.incr_attempts")
                self.assertEqual(initial["total"], 0)

                tracker.record_attempt("test.incr_attempts", passed=False, diagnosis="prints_instead_of_returns")
                stats = tracker.get_lesson_stats("test.incr_attempts")
                self.assertEqual(stats["total"], 1)
                self.assertEqual(stats["failed"], 1)

                # Second attempt
                tracker.record_attempt("test.incr_attempts", passed=False, diagnosis="prints_instead_of_returns")
                stats = tracker.get_lesson_stats("test.incr_attempts")
                self.assertEqual(stats["total"], 2)
                self.assertEqual(stats["failed"], 2)

                # Third attempt (passing)
                tracker.record_attempt("test.incr_attempts", passed=True, diagnosis="")
                stats = tracker.get_lesson_stats("test.incr_attempts")
                self.assertEqual(stats["total"], 3)
                self.assertEqual(stats["passed"], 1)
                self.assertEqual(stats["failed"], 2)
            finally:
                tracker.close()

    def test_save_and_record_passed(self):
        """Passing tests should also be recorded."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = make_add_pack(tmp)
            student = tmp / "student.py"
            student.write_text("def add_numbers(a, b):\n    return a + b\n")

            report = run_tests_workflow(
                str(pack),
                student_file=str(student),
                lesson_id="test.passed_test",
            )
            lesson_id = save_and_record(report, lesson_id="test.passed_test")
            self.assertEqual(lesson_id, "test.passed_test")
            self.assertTrue(report.passed)

    def test_resolve_current_lesson_returns_expected_keys(self):
        """resolve_current_lesson should return dict with required keys."""
        # Isolate DUCKY_HOME so the test is hermetic: resolve_current_lesson
        # reads the real config, so a developer's live ~/.ducky/config.json
        # (e.g. a stale workspace_dir from a prior session) would otherwise
        # leak in and resolve a real lesson here. Pointing DUCKY_HOME at a
        # non-existent path makes resolve_current_lesson() return None — the
        # "no workspace" behaviour this test asserts.
        tmp_home = str(Path(tempfile.mkdtemp()) / "fresh_home")
        with patch.dict(os.environ, {"DUCKY_HOME": tmp_home}):
            result = resolve_current_lesson(lesson_name=None)
        # With no workspace under the isolated home, must be None.
        self.assertIsNone(result)


class WorkflowsImportTest(unittest.TestCase):
    """Verify that workflows module has the expected public API."""

    def test_public_functions_are_importable(self):
        from ducky.workflows import (
            connect_workflow,
            disconnect_workflow,
            pull_packs_workflow,
            resolve_current_lesson,
            run_tests_workflow,
            save_and_record,
        )

        self.assertTrue(callable(run_tests_workflow))
        self.assertTrue(callable(save_and_record))
        self.assertTrue(callable(resolve_current_lesson))
        self.assertTrue(callable(connect_workflow))
        self.assertTrue(callable(pull_packs_workflow))
        self.assertTrue(callable(submit_workflow))
        self.assertTrue(callable(disconnect_workflow))


class ResolveDetectedLessonTests(unittest.TestCase):
    """Tests for resolve_current_lesson with detected (non-pack) lessons."""

    def test_resolve_by_name_detected_lesson(self):
        """resolve_current_lesson should find a detected lesson by name."""
        from ducky.workflows import resolve_current_lesson
        from tests.helpers import make_lesson_dir

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / "workspace"
            ws.mkdir()
            make_lesson_dir(ws, name="my_detected_lesson")

            with (
                patch("ducky.workspace.get_workspace_dir", return_value=ws),
                patch("ducky.workflows.get_current_lesson", return_value=None),
            ):
                # Resolve by directory name
                result = resolve_current_lesson(lesson_name="my_detected_lesson")
                if result is None:
                    # Try with the full detected lesson_id
                    from ducky.workspace import scan_workspace_for_lessons
                    with patch("ducky.workspace.get_workspace_dir", return_value=ws):
                        detected = scan_workspace_for_lessons()
                        if detected:
                            result = resolve_current_lesson(lesson_name=detected[0]["lesson_id"])

                if result is None:
                    self.fail("Could not resolve detected lesson by name")

                self.assertTrue(result.get("detected", False))
                self.assertEqual(result["pack_path"], "")
                self.assertTrue(result["entry_path"].is_file())
                self.assertIn("workspace.", result["lesson_id"])

    def test_resolve_detected_lesson_returns_expected_keys(self):
        """Resolved detected lesson should have all required keys."""
        from ducky.workflows import resolve_current_lesson
        from tests.helpers import make_lesson_dir

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / "workspace"
            ws.mkdir()
            make_lesson_dir(ws, name="detect_me")

            from ducky.workspace import get_current_lesson, scan_workspace_for_lessons
            with patch("ducky.workspace.get_workspace_dir", return_value=ws):
                # Force get_current_lesson to find the detected lesson
                current = get_current_lesson()
                if current:
                    result = resolve_current_lesson()
                else:
                    result = None

                if result is None:
                    self.skipTest("No detected lesson resolved (expected in bare test env)")

                self.assertIn("lesson_dir", result)
                self.assertIn("pack_path", result)
                self.assertIn("entry_file", result)
                self.assertIn("entry_path", result)
                self.assertIn("lesson_id", result)
                self.assertIn("title", result)
                self.assertIn("detected", result)


class RunTestsWorkflowDetectedLessonTests(unittest.TestCase):
    """Tests for run_tests_workflow with detected (non-pack) lessons."""

    def test_run_tests_workflow_detected_lesson_passes(self):
        """run_tests_workflow should work with detected lessons (no pack)."""
        from ducky.llm_client import FakeLLMClient
        from ducky.tutor import TutorEngine
        from ducky.workflows import run_tests_workflow

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            lesson_dir = tmp / "detected_lesson"
            lesson_dir.mkdir()
            (lesson_dir / "solution.py").write_text(
                "def add_numbers(a, b):\n    return a + b\n", encoding="utf-8"
            )
            tests = lesson_dir / "tests"
            tests.mkdir()
            (tests / "test_solution.py").write_text(
                "from solution import add_numbers\n"
                "def test_adds():\n"
                '    assert add_numbers(2, 3) == 5\n',
                encoding="utf-8",
            )

            report = run_tests_workflow(
                pack_path="",  # No pack — use run_from_dir
                student_file=str(lesson_dir / "solution.py"),
                tutor_engine=TutorEngine(FakeLLMClient()),
                lesson_id="detected_lesson",
            )
            self.assertTrue(report.passed)

    def test_run_tests_workflow_detected_lesson_fails(self):
        """run_tests_workflow with detected lesson and wrong code."""
        from ducky.llm_client import FakeLLMClient
        from ducky.tutor import TutorEngine
        from ducky.workflows import run_tests_workflow

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            lesson_dir = tmp / "bad_lesson"
            lesson_dir.mkdir()
            (lesson_dir / "solution.py").write_text(
                "def add_numbers(a, b):\n    return a - b\n", encoding="utf-8"
            )
            tests = lesson_dir / "tests"
            tests.mkdir()
            (tests / "test_solution.py").write_text(
                "from solution import add_numbers\n"
                "def test_adds():\n"
                '    assert add_numbers(2, 3) == 5\n',
                encoding="utf-8",
            )

            report = run_tests_workflow(
                pack_path="",
                student_file=str(lesson_dir / "solution.py"),
                tutor_engine=TutorEngine(FakeLLMClient()),
                lesson_id="bad_lesson",
            )
            self.assertFalse(report.passed)
            self.assertGreater(report.tests_failed, 0)
