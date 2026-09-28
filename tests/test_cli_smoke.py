import io
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from ducky.cli import main
from ducky.config import load_config
from ducky.progress import ProgressTracker
from ducky.teacher_backend import TeacherBackendClient
from tests.helpers import make_add_pack


@contextmanager
def temporary_ducky_home(path: Path):
    old_home = os.environ.get("DUCKY_HOME")
    os.environ["DUCKY_HOME"] = str(path)
    try:
        yield path
    finally:
        if old_home:
            os.environ["DUCKY_HOME"] = old_home
        elif "DUCKY_HOME" in os.environ:
            del os.environ["DUCKY_HOME"]


class CliCheckTests(unittest.TestCase):
    def test_check_correct_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = make_add_pack(tmp)
            student = tmp / "student.py"
            student.write_text("def add_numbers(a, b):\n    return a + b\n")
            with temporary_ducky_home(tmp / "ducky_home"):
                rc = main(["check", "--pack", str(pack), "--submission", str(student)])
            self.assertEqual(rc, 0)

    def test_check_wrong_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = make_add_pack(tmp)
            student = tmp / "student.py"
            student.write_text("def add_numbers(a, b):\n    print(a + b)\n")
            with temporary_ducky_home(tmp / "ducky_home"):
                rc = main(["check", "--pack", str(pack), "--submission", str(student)])
            self.assertEqual(rc, 1)

    def test_check_saves_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = make_add_pack(tmp)
            student = tmp / "student.py"
            student.write_text("def add_numbers(a, b):\n    return a + b\n")
            with temporary_ducky_home(tmp / "ducky_home"):
                rc = main(["check", "--pack", str(pack), "--submission", str(student), "--save-report"])
            self.assertEqual(rc, 0)

    def test_check_records_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = make_add_pack(tmp)
            student = tmp / "student.py"
            student.write_text("def add_numbers(a, b):\n    return a + b\n")
            home = tmp / "ducky_home"
            with temporary_ducky_home(home):
                rc = main(["check", "--pack", str(pack), "--submission", str(student)])
                tracker = ProgressTracker(home / "progress.db")
                try:
                    stats = tracker.get_lesson_stats("python_basics.lesson_01")
                finally:
                    tracker.close()
            self.assertEqual(rc, 0)
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["passed"], 1)


class CliPackTests(unittest.TestCase):
    def test_pack_inspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = make_add_pack(tmp)
            rc = main(["pack", "inspect", str(pack)])
            self.assertEqual(rc, 0)

    def test_pack_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "lesson"
            starter = source / "starter"
            tests = source / "tests"
            starter.mkdir(parents=True)
            tests.mkdir(parents=True)
            (starter / "main.py").write_text("def f(): pass\n")
            (tests / "test_main.py").write_text("import unittest\n")
            out = tmp / "out.duckypack"
            rc = main(
                [
                    "pack",
                    "create",
                    "--source",
                    str(source),
                    "--output",
                    str(out),
                    "--lesson-id",
                    "test.lesson",
                    "--title",
                    "Test",
                    "--entry-file",
                    "main.py",
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(out.exists())


class CliReportTests(unittest.TestCase):
    def test_report_list_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            import os

            old_home = os.environ.get("DUCKY_HOME")
            os.environ["DUCKY_HOME"] = tmp
            try:
                from ducky.reports import ReportStore

                ReportStore(tmp)  # ensure dirs exist
                rc = main(["report", "list"])
                self.assertEqual(rc, 0)
            finally:
                if old_home:
                    os.environ["DUCKY_HOME"] = old_home
                elif "DUCKY_HOME" in os.environ:
                    del os.environ["DUCKY_HOME"]


class CliRedactTests(unittest.TestCase):
    def test_redact_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            code_file = tmp / "code.py"
            code_file.write_text("email me at test@example.com from /Users/testuser/file.py\n")
            rc = main(["redact", "--input", str(code_file)])
            self.assertEqual(rc, 0)


class CliDatasetTests(unittest.TestCase):
    def test_dataset_preview_missing_file(self):
        rc = main(["dataset", "preview", "--input", "/nonexistent/file.jsonl"])
        self.assertEqual(rc, 1)

    def test_dataset_stats_missing_file(self):
        rc = main(["dataset", "stats", "--input", "/nonexistent/file.jsonl"])
        self.assertEqual(rc, 1)


class CliStudentTests(unittest.TestCase):
    def test_student_submit_and_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pack = make_add_pack(tmp)
            student = tmp / "student.py"
            student.write_text("def add_numbers(a, b):\n    return a + b\n")
            fetch_output = tmp / "latest.py"
            home = tmp / "ducky_home"

            def submit_side_effect_raw(report, *, student_name="", include_raw=False):
                self.assertEqual(report.lesson_id, "python_basics.lesson_01")
                self.assertEqual(student_name, "Ada")
                self.assertTrue(include_raw)
                self.assertTrue(report.raw_code)
                return {"ok": True, "status": "queued"}

            def fetch_side_effect(*, output_path=None):
                output = Path(output_path)
                output.write_text("def add_numbers(a, b):\n    return a + b\n", encoding="utf-8")
                return output

            with (
                temporary_ducky_home(home),
                patch.object(TeacherBackendClient, "submit_report", side_effect=submit_side_effect_raw) as submit_mock,
                patch.object(TeacherBackendClient, "fetch_file", side_effect=fetch_side_effect) as fetch_mock,
            ):
                rc = main(
                    [
                        "student",
                        "--pack",
                        str(pack),
                        "--submission",
                        str(student),
                        "--submit",
                        "--fetch",
                        "--fetch-output",
                        str(fetch_output),
                        "--student-name",
                        "Ada",
                        "--teacher-submit-url",
                        "https://teacher.example/api/submit",
                        "--teacher-fetch-url",
                        "https://teacher.example/api/latest.py",
                        "--teacher-code",
                        "123456",
                        "--teacher-upload-all",
                    ]
                )

            self.assertEqual(rc, 0)
            self.assertTrue(submit_mock.called)
            self.assertTrue(fetch_mock.called)
            self.assertTrue(fetch_output.exists())
            self.assertIn("return a + b", fetch_output.read_text(encoding="utf-8"))

    def test_config_set_persists_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "ducky_home"
            with temporary_ducky_home(home):
                rc = main(
                    [
                        "config",
                        "set",
                        "--llm-backend",
                        "ollama",
                        "--ollama-model",
                        "qwen2.5-coder:7b",
                        "--teacher-submit-url",
                        "https://teacher.example/api/submit",
                        "--teacher-code",
                        "123456",
                        "--teacher-upload-all",
                        "--student-name",
                        "Ada",
                    ]
                )

            self.assertEqual(rc, 0)
            config = load_config(home)
            self.assertEqual(config["llm_backend"], "ollama")
            self.assertEqual(config["ollama_model"], "qwen2.5-coder:7b")
            self.assertEqual(config["teacher_submit_url"], "https://teacher.example/api/submit")
            self.assertEqual(config["teacher_token"], "123456")
            self.assertEqual(config["teacher_access_code"], "123456")
            self.assertTrue(config["teacher_upload_all"])
            self.assertEqual(config["student_name"], "Ada")

    def test_config_set_can_disable_upload_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "ducky_home"
            with temporary_ducky_home(home):
                rc = main(
                    [
                        "config",
                        "set",
                        "--teacher-upload-all",
                    ]
                )
                rc2 = main(
                    [
                        "config",
                        "set",
                        "--no-teacher-upload-all",
                    ]
                )

            self.assertEqual(rc, 0)
            self.assertEqual(rc2, 0)
            config = load_config(home)
            self.assertFalse(config["teacher_upload_all"])


class CliQuizTests(unittest.TestCase):
    """Regression coverage for the CLI quiz input/answer handling.

    These guard two fixes that previously lived only in the GUI path:
      * the CLI must accept BOTH numeric (1..N) and uppercase letter
        (A..A+N-1) multiple-choice selections, and
      * command detection (b=back, q=quit) must be case-sensitive so an
        uppercase option letter is never misread as "back".
    """

    def _run_quiz(self, inputs, quiz, is_passed=True):
        """Drive ``_handle_quiz`` with scripted stdin and a fake quiz.

        ``inputs`` is the ordered list of ``input()`` responses. Returns the
        captured recorded (quiz_id, lesson_id, result, answers) tuple and the
        stdout text.
        """
        from unittest.mock import MagicMock, patch

        from ducky.cli import student_commands as sc
        from ducky.quiz import QuizResultsStore

        recorded = {}

        class FakeStore:
            def get_stats(self, quiz_id):
                return {
                    "attempts": 0,
                    "best": 0,
                    "last_correct": 0,
                    "last_passed": False,
                }

            def record(self, quiz_id, lesson_id, result, answers):
                recorded["call"] = (quiz_id, lesson_id, result, answers)

            def close(self):
                pass

        lesson = {
            "lesson_dir": Path("/tmp/lessons/python_basics.lesson_01"),
            "lesson_id": "python_basics.lesson_01",
            "pack_path": None,
        }

        with patch("ducky.workflows.resolve_current_lesson", return_value=lesson), patch(
            "ducky.quiz.load_student_quiz", return_value=(quiz, False)
        ), patch("ducky.quiz.QuizResultsStore", return_value=FakeStore()), patch(
            "builtins.input", side_effect=inputs
        ), patch.object(
            sc, "_maybe_submit_quiz_result"
        ), patch(
            "sys.stdout"
        ) as out:
            # Capture real stdout by redirecting through a StringIO.
            import io

            buf = io.StringIO()
            out.write = buf.write
            rc = sc._handle_quiz(MagicMock(lesson=None, status=False))

        return recorded, buf.getvalue(), rc

    def test_cli_quiz_accepts_uppercase_letter_selection(self):
        from ducky.quiz import parse_quiz_dict

        quiz = parse_quiz_dict(
            {
                "quiz_id": "quiz.test",
                "lesson_id": "python_basics.lesson_01",
                "title": "Letter Test",
                "questions": [
                    {
                        "id": "q1",
                        "type": "multiple_choice",
                        "prompt": "Pick B?",
                        "options": ["alpha", "bravo", "charlie"],
                        "correct": "bravo",
                    }
                ],
            },
            quiz_id="quiz.test",
            lesson_id="python_basics.lesson_01",
        )
        recorded, out, rc = self._run_quiz(["B", "q"], quiz)
        self.assertIn("call", recorded, "quiz must have recorded an attempt")
        _, _, result, answers = recorded["call"]
        # The recorded answer must be the canonical option text, not "B".
        self.assertEqual(answers.get("q1"), "bravo")
        # A 'q' at the second prompt quits with exit 0.
        self.assertEqual(rc, 0)

    def test_cli_quiz_uppercase_letter_is_not_misread_as_back(self):
        from ducky.quiz import parse_quiz_dict

        quiz = parse_quiz_dict(
            {
                "quiz_id": "quiz.test",
                "lesson_id": "python_basics.lesson_01",
                "title": "Back Collision Test",
                "questions": [
                    {
                        "id": "q1",
                        "type": "multiple_choice",
                        "prompt": "Pick B?",
                        "options": ["alpha", "bravo", "charlie"],
                        "correct": "bravo",
                    },
                    {
                        "id": "q2",
                        "type": "true_false",
                        "prompt": "True?",
                        "correct": "true",
                    },
                ],
            },
            quiz_id="quiz.test",
            lesson_id="python_basics.lesson_01",
        )
        # First answer 'B' (option 2) must NOT trigger back; then 'true' for q2;
        # then 'q' to quit. If 'B' were misread as back, q1 would never be
        # answered and the flow would re-ask it forever (or break).
        recorded, out, rc = self._run_quiz(["B", "true", "q"], quiz)
        self.assertIn("call", recorded)
        _, _, result, answers = recorded["call"]
        self.assertEqual(answers.get("q1"), "bravo")
        self.assertEqual(answers.get("q2"), "true")

    def test_cli_quiz_numeric_and_letter_both_record_correct_option(self):
        from ducky.quiz import parse_quiz_dict

        quiz = parse_quiz_dict(
            {
                "quiz_id": "quiz.test",
                "lesson_id": "python_basics.lesson_01",
                "title": "Mixed Input Test",
                "questions": [
                    {
                        "id": "q1",
                        "type": "multiple_choice",
                        "prompt": "Pick two?",
                        "options": ["one", "two", "three"],
                        "correct": "two",
                    }
                ],
            },
            quiz_id="quiz.test",
            lesson_id="python_basics.lesson_01",
        )
        # Feed numeric '2' AND uppercase 'B' in separate runs; both must resolve
        # to the canonical option text (two / alpha respectively), proving the
        # parser accepts either input style. A lowercase 'b' is the back command
        # and must NOT be parsed as an option.
        recorded, out, rc = self._run_quiz(["2", "q"], quiz)
        self.assertEqual(recorded["call"][3].get("q1"), "two")
        recorded2, out2, rc2 = self._run_quiz(["B", "q"], quiz)
        self.assertEqual(recorded2["call"][3].get("q1"), "two")

    def test_cli_quiz_lowercase_b_is_back_not_option(self):
        from ducky.quiz import parse_quiz_dict

        quiz = parse_quiz_dict(
            {
                "quiz_id": "quiz.test",
                "lesson_id": "python_basics.lesson_01",
                "title": "Lowercase B Collision Test",
                "questions": [
                    {
                        "id": "q1",
                        "type": "multiple_choice",
                        "prompt": "Pick two?",
                        "options": ["one", "two", "three"],
                        "correct": "two",
                    },
                    {
                        "id": "q2",
                        "type": "true_false",
                        "prompt": "True?",
                        "correct": "true",
                    },
                ],
            },
            quiz_id="quiz.test",
            lesson_id="python_basics.lesson_01",
        )
        # 'b' at Q1 must be treated as BACK (a no-op at the first question),
        # NOT as option B. Then 'A' selects option 0 ('one'); 'true' for Q2;
        # 'q' quits. If 'b' were parsed as option B, Q1 would record 'two' and
        # the flow would never re-ask Q1 — so the recorded answer must be 'one'.
        recorded, out, rc = self._run_quiz(["b", "A", "true", "q"], quiz)
        self.assertIn("call", recorded)
        _, _, result, answers = recorded["call"]
        self.assertEqual(answers.get("q1"), "one")
        self.assertEqual(answers.get("q2"), "true")

    def test_cli_quiz_invalid_mcq_rejected_not_recorded_as_garbage(self):
        """Typing '99' for a 3-option MCQ must NOT be recorded as the answer.

        This is the regression: the old CLI fell through and stored the raw
        '99' as the student's choice, so the auto-grader silently marked it
        wrong and the stored result was garbage. The fix re-prompts on any
        non-option input, so the recorded answer must be the *valid option text*.
        """
        from ducky.quiz import parse_quiz_dict

        quiz = parse_quiz_dict(
            {
                "quiz_id": "quiz.test",
                "lesson_id": "python_basics.lesson_01",
                "title": "Invalid MCQ Test",
                "questions": [
                    {
                        "id": "q1",
                        "type": "multiple_choice",
                        "prompt": "Pick?",
                        "options": ["one", "two", "three"],
                        "correct": "two",
                    },
                    {
                        "id": "q2",
                        "type": "true_false",
                        "prompt": "True?",
                        "correct": "true",
                    },
                ],
            },
            quiz_id="quiz.test",
            lesson_id="python_basics.lesson_01",
        )
        # '99' (invalid) then 'B' (option two) for q1; 'false' for q2; no 'q'
        # because the quiz records immediately once all questions are answered.
        recorded, out, rc = self._run_quiz(["99", "B", "false"], quiz)
        self.assertIn("call", recorded)
        _, _, result, answers = recorded["call"]
        # The recorded q1 must be the canonical option text 'two', never '99'.
        self.assertEqual(answers.get("q1"), "two")
        self.assertEqual(answers.get("q2"), "false")
        self.assertIn("Please enter a number", out)

    def test_cli_quiz_unparseable_true_false_reprompts(self):
        """An answer that is neither true/false nor a command ('maybe') must be
        re-prompted rather than recorded as the student's true_false answer."""
        from ducky.quiz import parse_quiz_dict

        quiz = parse_quiz_dict(
            {
                "quiz_id": "quiz.test",
                "lesson_id": "python_basics.lesson_01",
                "title": "TF Parse Test",
                "questions": [
                    {
                        "id": "q1",
                        "type": "true_false",
                        "prompt": "True?",
                        "correct": "true",
                    },
                ],
            },
            quiz_id="quiz.test",
            lesson_id="python_basics.lesson_01",
        )
        recorded, out, rc = self._run_quiz(["maybe", "false"], quiz)
        self.assertIn("call", recorded)
        _, _, result, answers = recorded["call"]
        self.assertEqual(answers.get("q1"), "false")
        self.assertIn("Please answer true or false", out)

    def test_cli_quiz_status_shows_weak_spots(self):
        """``ducky quiz --status`` must surface per-question weak spots (and
        explanations, when authored) by re-grading stored attempts against the
        current quiz — not just aggregate attempts/best/last."""
        import tempfile

        from ducky.cli import student_commands as sc
        from ducky.quiz import QuizResultsStore, grade_quiz, parse_quiz_dict

        quiz = parse_quiz_dict(
            {
                "quiz_id": "quiz.test",
                "lesson_id": "python_basics.lesson_01",
                "title": "Weak Spot Test",
                "questions": [
                    {
                        "id": "q1",
                        "type": "multiple_choice",
                        "prompt": "Pick two?",
                        "options": ["one", "two", "three"],
                        "correct": "two",
                        "explanation": "Two is the second value.",
                    },
                    {
                        "id": "q2",
                        "type": "true_false",
                        "prompt": "True?",
                        "correct": "true",
                    },
                ],
            },
            quiz_id="quiz.test",
            lesson_id="python_basics.lesson_01",
        )

        db = tempfile.mkdtemp()
        store = QuizResultsStore(db_path=Path(db) / "quiz_results.db")
        try:
            # Two attempts, both miss q1 (the student keeps picking 'one').
            store.record(
                quiz.quiz_id,
                quiz.lesson_id,
                grade_quiz(quiz, {"q1": "one", "q2": "true"}, questions=quiz.questions),
                {"q1": "one", "q2": "true"},
            )
            store.record(
                quiz.quiz_id,
                quiz.lesson_id,
                grade_quiz(quiz, {"q1": "one", "q2": "true"}, questions=quiz.questions),
                {"q1": "one", "q2": "true"},
            )
        finally:
            store.close()

        lesson = {
            "lesson_dir": Path("/tmp/lessons/python_basics.lesson_01"),
            "lesson_id": "python_basics.lesson_01",
            "pack_path": None,
        }
        buf = io.StringIO()
        with patch("ducky.workflows.resolve_current_lesson", return_value=lesson), patch(
            "ducky.quiz.load_student_quiz", return_value=(quiz, False)
        ), patch("ducky.quiz.QuizResultsStore", return_value=QuizResultsStore(db_path=Path(db) / "quiz_results.db")), patch(
            "sys.stdout"
        ) as out, patch.object(sc, "_maybe_submit_quiz_result"):
            out.write = buf.write
            rc = sc._handle_quiz(MagicMock(lesson=None, status=True))

        text = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Weak spots", text)
        self.assertIn("q1", text)  # the missed question id is reported
        self.assertIn("Two is the second value.", text)  # its explanation surfaces
