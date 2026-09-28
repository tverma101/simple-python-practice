"""Comprehensive student GUI simulation tests.

Tests mimic real student behavior through the Ducky terminal UI:
- Full student journey: connect → pull → start → test → feedback
- Diagnostic feedback quality for various failure types
- Function name mismatch detection with fuzzy suggestions
- Hint escalation across multiple attempts
- Navigation flow and edge cases
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# ── Test fixtures ────────────────────────────────────────────────────


@pytest.fixture
def student_home(tmp_path, monkeypatch):
    """Create a temporary student home directory with config."""
    home = tmp_path / "student_home"
    home.mkdir()
    config_dir = home / ".ducky"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"auto_update": False, "llm_backend": "none"})
    )
    # Isolate workspace dir to per-test tmp dir
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    monkeypatch.setenv("DUCKY_WORKSPACE", str(ws_dir))
    # Also patch HOME so os.environ["HOME"] in tests doesn't leak
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def teacher_home(tmp_path):
    """Create a temporary teacher home directory with DB and packs."""
    home = tmp_path / "teacher_home"
    home.mkdir()
    config_dir = home / ".ducky"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"auto_update": False, "llm_backend": "none"})
    )
    return home


@pytest.fixture
def grade_calc_pack(tmp_path):
    """Create a grade calculator .duckypack for testing."""
    from ducky.pack import DuckyManifest, create_duckypack

    starter = tmp_path / "starter"
    tests = tmp_path / "tests"
    starter.mkdir()
    tests.mkdir()

    # Empty stub — student writes from scratch
    (starter / "grade_calculator.py").write_text(
        "# Grade Calculator\n"
        "# Complete all functions below.\n"
        "def main():\n"
        "    pass\n"
    )

    # Test file with clear function expectations
    (tests / "test_grade_calculator.py").write_text(
        'import unittest\n'
        'from grade_calculator import calculate_average, determine_letter_grade, calculate_gpa\n\n'
        '\n'
        'class TestCalculateAverage(unittest.TestCase):\n'
        '    def test_single_grade(self):\n'
        '        self.assertAlmostEqual(calculate_average([85]), 85.0)\n'
        '    def test_multiple_grades(self):\n'
        '        self.assertAlmostEqual(calculate_average([80, 90, 70]), 80.0)\n'
        '    def test_empty_list(self):\n'
        '        self.assertEqual(calculate_average([]), 0.0)\n'
        '\n'
        '\n'
        'class TestDetermineLetterGrade(unittest.TestCase):\n'
        '    def test_grade_a(self):\n'
        '        self.assertEqual(determine_letter_grade(95), "A")\n'
        '    def test_grade_b(self):\n'
        '        self.assertEqual(determine_letter_grade(85), "B")\n'
        '    def test_grade_c(self):\n'
        '        self.assertEqual(determine_letter_grade(75), "C")\n'
        '    def test_grade_f(self):\n'
        '        self.assertEqual(determine_letter_grade(50), "F")\n'
        '\n'
        '\n'
        'class TestCalculateGPA(unittest.TestCase):\n'
        '    def test_all_a(self):\n'
        '        self.assertAlmostEqual(calculate_gpa([95, 98]), 4.0)\n'
        '    def test_mixed(self):\n'
        '        self.assertAlmostEqual(calculate_gpa([90, 80]), 3.5, places=1)\n'
    )

    manifest = DuckyManifest(
        lesson_id="grade_calculator",
        title="Grade Calculator",
        entry_file="grade_calculator.py",
        concept_tags=["functions", "lists", "conditionals"],
        instructions=(
            "# Grade Calculator\n\n"
            "Implement three functions:\n\n"
            "1. `calculate_average(grades)` — return the average of a list of numbers\n"
            "2. `determine_letter_grade(score)` — return A/B/C/D/F based on score\n"
            "3. `calculate_gpa(grades)` — convert grades to GPA points and return average\n"
        ),
    )

    out = tmp_path / "grade_calculator.duckypack"
    create_duckypack(starter, tests, out, manifest)
    return out


@pytest.fixture
def add_pack(tmp_path):
    """Create a simple add_numbers .duckypack for testing."""
    from ducky.pack import DuckyManifest, create_duckypack

    work = tmp_path / "add_pack_work"
    work.mkdir()
    starter = work / "starter"
    tests = work / "tests"
    starter.mkdir()
    tests.mkdir()
    (starter / "add_numbers.py").write_text("def add_numbers(a, b):\n    pass\n")
    (tests / "test_add_numbers.py").write_text(
        "import unittest\nfrom add_numbers import add_numbers\n\n"
        "class TestAddNumbers(unittest.TestCase):\n"
        "    def test_adds_two_numbers(self):\n        self.assertEqual(add_numbers(2, 3), 5)\n"
        "    def test_adds_zero(self):\n        self.assertEqual(add_numbers(0, 8), 8)\n"
    )
    manifest = DuckyManifest(
        lesson_id="01_add_numbers",
        title="Add Numbers",
        entry_file="add_numbers.py",
        concept_tags=["functions", "return_vs_print"],
        diagnostic_cases=[
            {"name": "adds two numbers", "function_name": "add_numbers", "args": [2, 3], "expected": 5},
            {"name": "adds zero", "function_name": "add_numbers", "args": [0, 8], "expected": 8},
        ],
    )
    out = work / "01_add_numbers.duckypack"
    create_duckypack(starter, tests, out, manifest)
    return out


# ── Helper: simulate student home with pack downloaded ──────────────


def setup_student_with_pack(student_home, pack_path, pack_name="lesson.duckypack"):
    """Copy a pack to the student's packs directory."""
    packs_dir = student_home / ".ducky" / "packs"
    packs_dir.mkdir(exist_ok=True)
    dest = packs_dir / pack_name
    import shutil

    shutil.copy2(pack_path, dest)
    return dest


def setup_student_config(student_home, teacher_url="", token="", name="Maya"):
    """Write student config with teacher connection details."""
    config_dir = student_home / ".ducky"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "auto_update": False,
                "llm_backend": "none",
                "teacher_url": teacher_url,
                "teacher_token": token,
                "student_name": name,
            }
        )
    )


# ════════════════════════════════════════════════════════════════════
# SECTION 1: Full Student Journey Through GUI
# ════════════════════════════════════════════════════════════════════


class TestStudentJourney:
    """Simulate a real student clicking through the GUI menus."""

    def test_student_starts_with_no_lessons(self, student_home, capsys):
        """Student opens Ducky for the first time — sees empty state."""
        # HOME already set by student_home fixture via monkeypatch
        from ducky.workspace import get_progress, list_available_lessons

        lessons = list_available_lessons()
        progress = get_progress()

        assert lessons == []
        assert progress["total"] == 0
        assert progress["completed"] == 0

    def test_student_connects_to_teacher(self, student_home, capsys):
        """Student navigates to Classroom > Connect to Teacher."""
        # HOME already set by student_home fixture via monkeypatch
        # Simulate teacher server running on localhost:8420
        # In real use, student would enter URL and code
        # Here we test the config save path
        from ducky.config import load_config, save_config
        from ducky.workflows import connect_workflow

        save_config(
            {
                "teacher_url": "http://localhost:8420",
                "teacher_token": "test_token_abc123",
                "student_name": "Maya",
            }
        )
        config = load_config()
        assert config["teacher_url"] == "http://localhost:8420"
        assert config["student_name"] == "Maya"

    def test_student_sees_pack_after_download(self, student_home, grade_calc_pack):
        """After pulling from teacher, student sees lessons in the list."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.workspace import get_progress, list_available_lessons

        lessons = list_available_lessons()
        progress = get_progress()

        # May also include example packs from examples/packs/
        gc = [l for l in lessons if l["lesson_id"] == "grade_calculator"]
        assert len(gc) == 1
        assert gc[0]["title"] == "Grade Calculator"
        assert gc[0]["status"] == "not_started"
        assert progress["total"] >= 1

    def test_student_starts_lesson_sees_instructions(
        self, student_home, grade_calc_pack, capsys
    ):
        """Student starts a lesson — sees instructions and code file location."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.workspace import get_lesson_instructions, start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        lesson_dir = start_lesson(pack_path)

        # Lesson directory was created
        assert lesson_dir.is_dir()

        # Entry file exists (the stub)
        entry = lesson_dir / "grade_calculator.py"
        assert entry.is_file()
        content = entry.read_text()
        assert "def main():" in content
        assert "pass" in content

        # INSTRUCTIONS.md was created
        instructions_file = lesson_dir / "INSTRUCTIONS.md"
        assert instructions_file.is_file()
        instructions = instructions_file.read_text()
        assert "Grade Calculator" in instructions
        assert "calculate_average" in instructions

        # Instructions are accessible via workspace API
        instr = get_lesson_instructions("grade_calculator")
        assert instr is not None
        assert "calculate_average" in instr

    def test_student_runs_tests_on_empty_stub(
        self, student_home, grade_calc_pack
    ):
        """Student runs tests on the empty stub — gets function_not_found errors."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.reports import render_text_report
        from ducky.runner import run_pack
        from ducky.workspace import start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        lesson_dir = start_lesson(pack_path)
        entry = lesson_dir / "grade_calculator.py"

        report = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)

        # All tests fail — functions don't exist
        assert not report.passed
        assert report.tests_passed == 0
        assert report.tests_failed > 0

        # Every failure is diagnosed as function_not_found
        for failure in report.failures:
            assert failure.diagnosis == "function_not_found"

        # Report renders without crashing
        text = render_text_report(report)
        assert "Grade Calculator" in text
        assert "fail" in text.lower() or "failed" in text.lower()

    def test_student_writes_partial_code_retests(
        self, student_home, grade_calc_pack
    ):
        """Student implements calculate_average, re-runs — some tests pass."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.runner import run_pack
        from ducky.workspace import start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        lesson_dir = start_lesson(pack_path)
        entry = lesson_dir / "grade_calculator.py"

        # Student implements one function correctly
        entry.write_text(
            "# Grade Calculator\n"
            "def calculate_average(grades):\n"
            "    if not grades:\n"
            "        return 0.0\n"
            "    return sum(grades) / len(grades)\n"
            "\n"
            "def determine_letter_grade(score):\n"
            "    pass\n"
            "\n"
            "def calculate_gpa(grades):\n"
            "    pass\n"
            "\n"
            "def main():\n"
            "    pass\n"
        )

        report = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)

        # Some tests pass (calculate_average), some fail
        assert report.tests_passed > 0
        assert report.tests_failed > 0

        # failures list only contains failed tests — passing tests are counted
        # Check that calculate_average tests pass (they're the first 3 in the test file)
        assert report.tests_passed >= 2  # At least 2 calculate_average tests pass

    def test_student_completes_all_functions(
        self, student_home, grade_calc_pack
    ):
        """Student implements all functions correctly — all tests pass."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.reports import render_text_report
        from ducky.runner import run_pack
        from ducky.workspace import start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        lesson_dir = start_lesson(pack_path)
        entry = lesson_dir / "grade_calculator.py"

        # Student implements everything correctly
        entry.write_text(
            "def calculate_average(grades):\n"
            "    if not grades:\n"
            "        return 0.0\n"
            "    return sum(grades) / len(grades)\n"
            "\n"
            "def determine_letter_grade(score):\n"
            "    if score >= 90:\n"
            "        return 'A'\n"
            "    elif score >= 80:\n"
            "        return 'B'\n"
            "    elif score >= 70:\n"
            "        return 'C'\n"
            "    elif score >= 60:\n"
            "        return 'D'\n"
            "    return 'F'\n"
            "\n"
            "def calculate_gpa(grades):\n"
            "    if not grades:\n"
            "        return 0.0\n"
            "    grade_points = []\n"
            "    for g in grades:\n"
            "        if g >= 90:\n"
            "            grade_points.append(4.0)\n"
            "        elif g >= 80:\n"
            "            grade_points.append(3.0)\n"
            "        elif g >= 70:\n"
            "            grade_points.append(2.0)\n"
            "        elif g >= 60:\n"
            "            grade_points.append(1.0)\n"
            "        else:\n"
            "            grade_points.append(0.0)\n"
            "    return sum(grade_points) / len(grade_points)\n"
            "\n"
            "def main():\n"
            "    pass\n"
        )

        report = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)
        text = render_text_report(report)

        assert report.passed
        assert report.tests_failed == 0
        assert "All tests passed" in text or "passed" in text.lower()

    def test_lesson_marked_completed_after_pass(
        self, student_home, grade_calc_pack
    ):
        """After all tests pass, lesson is marked completed."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.workspace import list_available_lessons, mark_test_result, start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        start_lesson(pack_path)

        # Before passing
        lessons = list_available_lessons()
        assert lessons[0]["status"] == "in_progress"

        # Mark as passed
        mark_test_result("grade_calculator", True)

        # After passing
        lessons = list_available_lessons()
        assert lessons[0]["status"] == "completed"


# ════════════════════════════════════════════════════════════════════
# SECTION 2: Diagnostic Feedback Quality
# ════════════════════════════════════════════════════════════════════


class TestDiagnosticFeedback:
    """Verify that students get useful, actionable feedback for each failure type."""

    def test_function_not_found_feedback(self):
        """When student defines no functions, feedback names the missing function."""
        from ducky.diagnostics import classify_failure, hint_for_diagnosis

        diagnosis = classify_failure(
            expected=5,
            actual=None,
            error="AttributeError: module 'grade_calculator' has no attribute 'calculate_average'",
        )
        assert diagnosis == "function_not_found"

        hint = hint_for_diagnosis(
            "function_not_found",
            {"expected_fn": "calculate_average"},
            level=1,
        )
        assert "calculate_average" in hint
        assert "not found" in hint.lower() or "missing" in hint.lower() or "expected" in hint.lower()

    def test_function_name_mismatch_with_suggestion(self):
        """When student names function slightly wrong, feedback suggests the right name."""
        from ducky.function_detector import detect_function_mismatch

        student_code = "def calc_average(grades):\n    return sum(grades) / len(grades)\n"
        test_code = "from grade_calculator import calculate_average\nresult = calculate_average([80, 90])"

        mismatch = detect_function_mismatch(
            student_code=student_code,
            test_code=test_code,
        )

        assert mismatch["mismatch"] is True
        assert "calculate_average" in mismatch["missing"]
        assert "calc_average" in mismatch["suggestions"].get("calculate_average", "")
        # Fuzzy match found: calc_average → calculate_average

    def test_function_name_mismatch_hint_level1(self):
        """Level 1 hint for function name mismatch tells student to rename."""
        from ducky.diagnostics import hint_for_diagnosis

        hint = hint_for_diagnosis(
            "function_not_found",
            {
                "expected_fn": "calculate_average",
                "closest_match": "calc_average",
            },
            level=1,
        )
        assert "calculate_average" in hint
        assert "calc_average" in hint
        assert "rename" in hint.lower() or "Rename" in hint

    def test_function_name_mismatch_hint_level2(self):
        """Level 2 hint gives specific rename instructions."""
        from ducky.diagnostics import hint_for_diagnosis

        hint = hint_for_diagnosis(
            "function_not_found",
            {
                "expected_fn": "calculate_average",
                "closest_match": "calc_average",
                "defined_functions": ["calc_average", "main"],
            },
            level=2,
        )
        assert "calculate_average" in hint
        assert "calc_average" in hint
        assert "Fix:" in hint or "rename" in hint.lower()

    def test_missing_function_no_match_hint(self):
        """When student defines wrong functions entirely, feedback says what's expected."""
        from ducky.diagnostics import hint_for_diagnosis

        hint = hint_for_diagnosis(
            "function_not_found",
            {
                "expected_fn": "calculate_average",
                "defined_functions": ["get_grade", "main"],
            },
            level=3,
        )
        assert "calculate_average" in hint
        assert "get_grade" in hint or "def calculate_average" in hint

    def test_syntax_error_feedback(self):
        """Syntax error gives clear, actionable feedback."""
        from ducky.diagnostics import classify_failure, hint_for_diagnosis

        diagnosis = classify_failure(
            error='File "solution.py", line 3\n    def calculate_average grades:\n                     ^\nSyntaxError: invalid syntax',
        )
        assert diagnosis == "syntax_error"

        hint = hint_for_diagnosis("syntax_error", level=1)
        assert "colon" in hint.lower() or "syntax" in hint.lower()

        hint2 = hint_for_diagnosis("syntax_error", level=2)
        assert "colon" in hint2.lower() or ":" in hint2

    def test_none_returned_feedback(self):
        """Function returns None — feedback says to add return statement."""
        from ducky.diagnostics import classify_failure, hint_for_diagnosis

        diagnosis = classify_failure(
            expected=85,
            actual=None,
            stdout="",
        )
        assert diagnosis == "none_returned"

        hint = hint_for_diagnosis("none_returned", level=1)
        assert "return" in hint.lower()

    def test_prints_instead_of_returns_feedback(self):
        """Student uses print() instead of return — feedback distinguishes them."""
        from ducky.diagnostics import classify_failure, hint_for_diagnosis

        diagnosis = classify_failure(
            expected=85,
            actual=None,
            stdout="85",
        )
        assert diagnosis == "prints_instead_of_returns"

        hint = hint_for_diagnosis("prints_instead_of_returns", level=1)
        assert "print" in hint.lower()
        assert "return" in hint.lower()

    def test_incorrect_logic_feedback(self):
        """Wrong answer — feedback encourages step-by-step debugging."""
        from ducky.diagnostics import classify_failure, hint_for_diagnosis

        diagnosis = classify_failure(
            expected="A",
            actual="B",
            error="AssertionError: assert 'B' == 'A'",
        )
        assert diagnosis == "incorrect_logic"

        hint = hint_for_diagnosis("incorrect_logic", level=1)
        assert "logic" in hint.lower() or "step" in hint.lower() or "walk through" in hint.lower()

    def test_wrong_type_feedback(self):
        """Return type mismatch — feedback suggests type conversion."""
        from ducky.diagnostics import classify_failure, hint_for_diagnosis

        diagnosis = classify_failure(
            expected=85,
            actual="85",
        )
        assert diagnosis == "wrong_type"

        hint = hint_for_diagnosis("wrong_type", level=1)
        assert "type" in hint.lower()

    def test_case_mismatch_feedback(self):
        """Wrong capitalization — feedback points to case issue."""
        from ducky.diagnostics import classify_failure, hint_for_diagnosis

        diagnosis = classify_failure(
            expected="Hello",
            actual="hello",
        )
        assert diagnosis == "case_mismatch"

        hint = hint_for_diagnosis("case_mismatch", level=1)
        assert "capitaliz" in hint.lower() or "case" in hint.lower()

    def test_extra_spaces_feedback(self):
        """Extra whitespace — feedback points to spacing."""
        from ducky.diagnostics import classify_failure, hint_for_diagnosis

        diagnosis = classify_failure(
            expected="hello",
            actual=" hello ",
        )
        assert diagnosis == "extra_spaces"

        hint = hint_for_diagnosis("extra_spaces", level=1)
        assert "space" in hint.lower() or "whitespace" in hint.lower()

    def test_list_order_wrong_feedback(self):
        """Right items, wrong order — feedback suggests sorting."""
        from ducky.diagnostics import classify_failure, hint_for_diagnosis

        diagnosis = classify_failure(
            expected=[1, 2, 3],
            actual=[3, 2, 1],
        )
        assert diagnosis == "list_order_wrong"

        hint = hint_for_diagnosis("list_order_wrong", level=1)
        assert "order" in hint.lower() or "sort" in hint.lower()


# ════════════════════════════════════════════════════════════════════
# SECTION 3: Function Name Mismatch Detection
# ════════════════════════════════════════════════════════════════════


class TestFunctionNameMismatch:
    """Test fuzzy matching and rename suggestions for function names."""

    def test_exact_match_no_mismatch(self):
        """Student defines correct function name — no mismatch detected."""
        from ducky.function_detector import detect_function_mismatch

        student_code = "def calculate_average(grades):\n    return sum(grades) / len(grades)\n"
        test_code = "from grade_calculator import calculate_average"

        result = detect_function_mismatch(
            student_code=student_code,
            test_code=test_code,
        )
        assert result["mismatch"] is False
        assert "calculate_average" not in result["missing"]

    def test_close_match_found(self):
        """Student uses abbreviated name — fuzzy match suggests full name."""
        from ducky.function_detector import detect_function_mismatch

        student_code = "def calc_avg(grades):\n    return 0\n"
        # Use bare function call pattern which the detector supports
        test_code = "result = calculate_average([80, 90])"

        result = detect_function_mismatch(
            student_code=student_code,
            test_code=test_code,
        )
        assert result["mismatch"] is True
        assert "calculate_average" in result["missing"]
        # calc_avg is close enough to calculate_average
        assert "calculate_average" in result["suggestions"]
        assert result["suggestions"]["calculate_average"] in ("calc_avg", "calc_average", "calculate_avg")

    def test_no_match_below_cutoff(self):
        """Student defines completely unrelated function — no fuzzy match."""
        from ducky.function_detector import detect_function_mismatch

        student_code = "def print_hello():\n    print('hi')\n"
        # Use bare function call pattern
        test_code = "result = calculate_average([80, 90])"

        result = detect_function_mismatch(
            student_code=student_code,
            test_code=test_code,
        )
        assert result["mismatch"] is True
        assert "calculate_average" in result["missing"]
        # print_hello is too different from calculate_average
        assert "calculate_average" not in result["suggestions"] or \
               result["suggestions"].get("calculate_average") != "print_hello"

    def test_multiple_missing_functions(self):
        """Student is missing multiple functions — all are detected."""
        from ducky.function_detector import detect_function_mismatch

        student_code = "def main():\n    pass\n"
        # Use bare function call patterns that the detector supports
        test_code = (
            "calculate_average([80, 90])\n"
            "determine_letter_grade(85)\n"
            "calculate_gpa([90, 80])\n"
        )

        result = detect_function_mismatch(
            student_code=student_code,
            test_code=test_code,
        )
        assert result["mismatch"] is True
        assert len(result["missing"]) >= 2
        assert "calculate_average" in result["missing"]
        assert "determine_letter_grade" in result["missing"]

    def test_extra_functions_not_flagged(self):
        """Student defines extra functions beyond what tests need — not flagged as mismatch."""
        from ducky.function_detector import detect_function_mismatch

        student_code = (
            "def calculate_average(grades):\n"
            "    return sum(grades) / len(grades)\n"
            "def helper():\n"
            "    pass\n"
        )
        # Use bare function call pattern
        test_code = "result = calculate_average([80, 90])"

        result = detect_function_mismatch(
            student_code=student_code,
            test_code=test_code,
        )
        assert result["mismatch"] is False
        assert "calculate_average" in result["defined"]

    def test_extract_function_names_from_source(self):
        """Extract function names from student code via AST."""
        from ducky.function_detector import extract_function_names

        code = (
            "def calculate_average(grades):\n"
            "    return sum(grades) / len(grades)\n"
            "\n"
            "def determine_letter_grade(score):\n"
            "    if score >= 90: return 'A'\n"
            "    return 'F'\n"
            "\n"
            "def main():\n"
            "    pass\n"
        )
        names = extract_function_names(code)
        assert "calculate_average" in names
        assert "determine_letter_grade" in names
        assert "main" in names
        assert len(names) == 3

    def test_extract_functions_from_syntax_error(self):
        """Extract function names from code with syntax error returns empty list."""
        from ducky.function_detector import extract_function_names

        code = "def broken(\n    pass"
        names = extract_function_names(code)
        assert names == []


# ════════════════════════════════════════════════════════════════════
# SECTION 4: Hint Escalation Across Attempts
# ════════════════════════════════════════════════════════════════════


class TestHintEscalation:
    """Hints get more detailed with each attempt."""

    def test_first_attempt_gentle_hint(self):
        """Attempt 1 — gentle nudge, not too much detail."""
        from ducky.diagnostics import hint_for_diagnosis

        hint = hint_for_diagnosis("none_returned", level=1)
        # Should mention return but not be overly prescriptive
        assert "return" in hint.lower()
        assert len(hint) < 300  # Not too long

    def test_third_attempt_specific_hint(self):
        """Attempt 3 — specific pointer."""
        from ducky.diagnostics import hint_for_diagnosis

        hint = hint_for_diagnosis("none_returned", level=2)
        assert "return" in hint.lower()
        assert len(hint) > 50  # More detailed

    def test_fifth_attempt_detailed_hint(self):
        """Attempt 5+ — near full guidance with code examples."""
        from ducky.diagnostics import hint_for_diagnosis

        hint = hint_for_diagnosis("none_returned", level=3)
        assert "return" in hint.lower()
        # Level 3 should include code-like guidance
        assert "return" in hint
        assert len(hint) > 100

    def test_function_mismatch_escalates_faster(self):
        """Function name mismatches get specific hints faster than other failures."""
        from ducky.diagnostics import hint_for_diagnosis
        from ducky.tutor import HintRequest, TutorEngine

        # At attempt 1, function mismatch should already be level 1
        # with specific rename guidance
        hint_level1 = hint_for_diagnosis(
            "function_not_found",
            {"expected_fn": "calculate_average", "closest_match": "calc_avg"},
            level=1,
        )
        assert "calculate_average" in hint_level1
        assert "calc_avg" in hint_level1

        # At level 2, should have Fix: instruction
        hint_level2 = hint_for_diagnosis(
            "function_not_found",
            {"expected_fn": "calculate_average", "closest_match": "calc_avg"},
            level=2,
        )
        assert "Fix:" in hint_level2 or "rename" in hint_level2.lower()

    def test_tutor_engine_escalation(self):
        """TutorEngine increases detail with attempt_number."""
        from ducky.llm_client import HintRequest
        from ducky.tutor import TutorEngine

        engine = TutorEngine()  # No LLM — rule-based only

        # Build a request for a none_returned failure
        request = HintRequest(
            lesson_id="test",
            title="Test",
            code_snippet="def foo(): pass",
            diagnosis="none_returned",
            failure_name="test_returns_value",
            expected=42,
            actual=None,
        )

        hint1 = engine.build_hint(request, attempt_number=1)
        hint3 = engine.build_hint(request, attempt_number=3)
        hint5 = engine.build_hint(request, attempt_number=5)

        # Hints should get progressively more detailed
        assert len(hint1) <= len(hint3) <= len(hint5)
        # All should mention return
        assert "return" in hint1.lower()
        assert "return" in hint5.lower()


# ════════════════════════════════════════════════════════════════════
# SECTION 5: Report Rendering Quality
# ════════════════════════════════════════════════════════════════════


class TestReportRendering:
    """Verify reports render correctly and show useful information."""

    def test_rich_report_shows_all_failures(
        self, student_home, grade_calc_pack
    ):
        """Rich report includes every failure with diagnosis and hint."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.display import render_failure_detail, render_text_report_rich
        from ducky.runner import run_pack
        from ducky.workspace import start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        lesson_dir = start_lesson(pack_path)
        entry = lesson_dir / "grade_calculator.py"

        report = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)
        rich_text = render_text_report_rich(report)

        # Should show each failure
        for failure in report.failures:
            assert failure.name in rich_text

        # Should show hints (diagnosis is internal, not rendered as text)
        assert "Hint:" in rich_text or "hint" in rich_text.lower()

    def test_rich_report_shows_progress_bar(
        self, student_home, grade_calc_pack
    ):
        """Failed test report includes a progress bar."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.display import render_text_report_rich
        from ducky.runner import run_pack
        from ducky.workspace import start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        lesson_dir = start_lesson(pack_path)
        entry = lesson_dir / "grade_calculator.py"

        report = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)
        rich_text = render_text_report_rich(report)

        # Should show pass/fail counts
        assert str(report.tests_passed) in rich_text
        assert str(report.tests_failed) in rich_text

    def test_plain_report_for_logs(
        self, student_home, grade_calc_pack
    ):
        """Plain report works for logging/HTML without ANSI codes."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.reports import render_text_report_plain
        from ducky.runner import run_pack
        from ducky.workspace import start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        lesson_dir = start_lesson(pack_path)
        entry = lesson_dir / "grade_calculator.py"

        report = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)
        plain_text = render_text_report_plain(report)

        # Should have no ANSI escape codes
        assert "\033[" not in plain_text
        # Should have key info
        assert "Grade Calculator" in plain_text
        assert "FAIL" in plain_text

    def test_report_saved_to_disk(
        self, student_home, grade_calc_pack
    ):
        """Report is saved as JSON and HTML after test run."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.reports import ReportStore
        from ducky.runner import run_pack
        from ducky.workspace import start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        lesson_dir = start_lesson(pack_path)
        entry = lesson_dir / "grade_calculator.py"

        report = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)
        store = ReportStore()
        paths = store.save(report)

        assert paths["json"].is_file()
        assert paths["html"].is_file()

        # JSON should be valid
        data = json.loads(paths["json"].read_text())
        assert "lesson_id" in data
        assert "passed" in data
        assert "failures" in data

        # HTML should be valid
        html = paths["html"].read_text()
        assert "<html>" in html.lower()
        assert "Grade Calculator" in html


# ════════════════════════════════════════════════════════════════════
# SECTION 6: GUI Navigation and Edge Cases
# ════════════════════════════════════════════════════════════════════


class TestNavigationEdgeCases:
    """Test navigation edge cases that a real student might hit."""

    def test_no_lessons_message(self, student_home, capsys):
        """When no lessons are available, student sees helpful message."""
        # HOME already set by student_home fixture via monkeypatch
        from ducky.workspace import list_available_lessons

        lessons = list_available_lessons()
        assert lessons == []

    def test_invalid_lesson_number(self, student_home, grade_calc_pack):
        """Student enters invalid lesson number — handled gracefully."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.workspace import list_available_lessons

        lessons = list_available_lessons()
        idx = 5 - 1  # Student enters "5" but only 1 lesson

        assert idx >= len(lessons)
        # GUI would show error message — we verify the list length

    def test_run_tests_without_starting_lesson(self, student_home, monkeypatch):
        """Student tries to run tests without starting a lesson — error handled."""
        # HOME already set by student_home fixture via monkeypatch
        from ducky import workspace

        # Mock get_current_lesson to return None (no lesson started)
        monkeypatch.setattr(workspace, "get_current_lesson", lambda: None)

        from ducky.workspace import get_current_lesson

        current = get_current_lesson()
        assert current is None  # No lesson started

    def test_run_tests_without_pack_loaded(self, student_home, capsys):
        """Grading workspace — run tests with no pack loaded shows an error.

        This screen is teacher-only (it lives on TeacherMenuMixin now, not
        StudentMenuMixin), so a student-facing simulation must target the
        teacher mixin. With current_pack unset it must error and return
        before touching the test runner.
        """
        from ducky.gui.teacher import TeacherMenuMixin

        mixin = TeacherMenuMixin()
        mixin.current_pack = None
        with patch("builtins.input", return_value=""):
            mixin._workspace_run_tests()
        assert "No pack loaded" in capsys.readouterr().out

    def test_connect_with_bad_code(self, student_home):
        """Student enters wrong access code — gets clear error."""
        # HOME already set by student_home fixture via monkeypatch
        from ducky.student_client import StudentClient, StudentClientError

        client = StudentClient(teacher_url="http://localhost:99999")
        with pytest.raises(StudentClientError):
            client.connect("000000000", "Maya")

    def test_lesson_already_completed_revisit(
        self, student_home, grade_calc_pack
    ):
        """Student revisits a completed lesson — lesson dir is preserved."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.workspace import mark_test_result, start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        lesson_dir = start_lesson(pack_path)

        # Complete it
        mark_test_result("grade_calculator", True)

        # Revisit — should return same dir without re-extracting
        lesson_dir2 = start_lesson(pack_path)
        assert lesson_dir == lesson_dir2

    def test_multiple_lessons_navigation(
        self, student_home, grade_calc_pack, add_pack
    ):
        """Student has multiple lessons — can navigate between them."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")
        setup_student_with_pack(student_home, add_pack, "01_add_numbers.duckypack")

        from ducky.workspace import list_available_lessons

        lessons = list_available_lessons()

        # Both our lessons should be visible (may also include example packs)
        lesson_ids = {l["lesson_id"] for l in lessons}
        assert "grade_calculator" in lesson_ids
        assert "01_add_numbers" in lesson_ids
        assert len(lessons) >= 2


# ════════════════════════════════════════════════════════════════════
# SECTION 7: End-to-End Student Journey with Teacher Server
# ════════════════════════════════════════════════════════════════════


class TestEndToEndWithTeacher:
    """Full E2E: teacher serves → student connects → pulls → tests → feedback."""

    def test_full_flow_connect_pull_start_test(
        self, student_home, teacher_home, grade_calc_pack, capsys
    ):
        """Complete student journey through teacher server."""
        from ducky.classroom import ClassroomDB
        from ducky.pack import read_manifest
        from ducky.server import DuckyServer

        # ── Teacher setup ──
        db = ClassroomDB(teacher_home / "teacher.db")
        cls = db.create_class("cs101", "CS 101", access_code="123456789")
        class_id = cls["class_id"]

        manifest = read_manifest(grade_calc_pack)
        pack_bytes = grade_calc_pack.read_bytes()
        db.add_pack(class_id, manifest.lesson_id, manifest.title, pack_bytes)

        # Start server
        server = DuckyServer(port=8499, access_code="123456789", class_id=class_id, db=db)
        server.start()
        time.sleep(1)

        try:
            import urllib.request

            # ── Student: connect ──
            # HOME already set by student_home fixture via monkeypatch
            req = urllib.request.Request(
                "http://localhost:8499/api/connect",
                data=json.dumps({"code": "123456789", "name": "Maya"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            resp = json.loads(urllib.request.urlopen(req).read())
            token = resp["token"]

            setup_student_config(
                student_home,
                teacher_url="http://localhost:8499",
                token=token,
                name="Maya",
            )

            # ── Student: list packs from server ──
            req = urllib.request.Request(
                "http://localhost:8499/api/packs",
                headers={"Authorization": f"Bearer {token}"},
            )
            packs_resp = json.loads(urllib.request.urlopen(req).read())
            packs = packs_resp.get("packs", packs_resp)
            assert len(packs) == 1
            assert packs[0]["lesson_id"] == "grade_calculator"

            # ── Student: download pack ──
            for p in packs:
                pack_id = p.get("pack_id", p.get("lesson_id", ""))
                req = urllib.request.Request(
                    f"http://localhost:8499/api/packs/{pack_id}/download",
                    headers={"Authorization": f"Bearer {token}"},
                )
                data = urllib.request.urlopen(req).read()
                packs_dir = student_home / ".ducky" / "packs"
                packs_dir.mkdir(exist_ok=True)
                (packs_dir / f"{pack_id}.duckypack").write_bytes(data)

            # ── Student: list available lessons locally ──
            from ducky.workspace import list_available_lessons

            lessons = list_available_lessons()
            gc = [l for l in lessons if l["lesson_id"] == "grade_calculator"]
            assert len(gc) == 1
            assert gc[0]["lesson_id"] == "grade_calculator"

            # ── Student: start lesson ──
            from ducky.workspace import start_lesson

            pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
            lesson_dir = start_lesson(pack_path)

            entry = lesson_dir / "grade_calculator.py"
            assert entry.is_file()
            assert "pass" in entry.read_text()  # Empty stub

            # ── Student: run tests — gets function_not_found ──
            from ducky.display import render_text_report_rich
            from ducky.reports import render_text_report
            from ducky.runner import run_pack

            report = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)
            assert not report.passed
            assert report.tests_failed > 0

            text = render_text_report(report)
            assert "Grade Calculator" in text

            # ── Student: writes code, retests — some pass ──
            entry.write_text(
                "def calculate_average(grades):\n"
                "    if not grades:\n"
                "        return 0.0\n"
                "    return sum(grades) / len(grades)\n"
                "\n"
                "def determine_letter_grade(score):\n"
                "    pass\n"
                "\n"
                "def calculate_gpa(grades):\n"
                "    pass\n"
                "\n"
                "def main():\n"
                "    pass\n"
            )
            report2 = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)
            assert report2.tests_passed > 0  # Some pass now

            # ── Student: completes all — all pass ──
            entry.write_text(
                "def calculate_average(grades):\n"
                "    if not grades:\n"
                "        return 0.0\n"
                "    return sum(grades) / len(grades)\n"
                "\n"
                "def determine_letter_grade(score):\n"
                "    if score >= 90:\n"
                "        return 'A'\n"
                "    elif score >= 80:\n"
                "        return 'B'\n"
                "    elif score >= 70:\n"
                "        return 'C'\n"
                "    elif score >= 60:\n"
                "        return 'D'\n"
                "    return 'F'\n"
                "\n"
                "def calculate_gpa(grades):\n"
                "    if not grades:\n"
                "        return 0.0\n"
                "    points = []\n"
                "    for g in grades:\n"
                "        if g >= 90:\n"
                "            points.append(4.0)\n"
                "        elif g >= 80:\n"
                "            points.append(3.0)\n"
                "        elif g >= 70:\n"
                "            points.append(2.0)\n"
                "        elif g >= 60:\n"
                "            points.append(1.0)\n"
                "        else:\n"
                "            points.append(0.0)\n"
                "    return sum(points) / len(points)\n"
                "\n"
                "def main():\n"
                "    pass\n"
            )
            report3 = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)
            assert report3.passed
            assert report3.tests_failed == 0

        finally:
            server.stop()
            db.close()

    def test_student_wrong_function_names_gets_rename_suggestion(
        self, student_home, grade_calc_pack
    ):
        """Student defines calc_avg instead of calculate_average — gets rename hint."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.reports import render_text_report
        from ducky.runner import run_pack
        from ducky.workspace import start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        lesson_dir = start_lesson(pack_path)
        entry = lesson_dir / "grade_calculator.py"

        # Student uses abbreviated names
        entry.write_text(
            "def calc_avg(grades):\n"
            "    return sum(grades) / len(grades)\n"
            "\n"
            "def get_letter(score):\n"
            "    return 'A'\n"
            "\n"
            "def get_gpa(grades):\n"
            "    return 4.0\n"
            "\n"
            "def main():\n"
            "    pass\n"
        )

        report = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)
        render_text_report(report)

        assert not report.passed

        # Check that at least one failure has a hint suggesting the rename
        hints_with_rename = [
            f.hint for f in report.failures
            if f.hint and ("calculate_average" in f.hint or "rename" in f.hint.lower())
        ]
        # Should have at least one hint mentioning the expected function name
        assert len(hints_with_rename) > 0, (
            f"Expected rename hints but got: {[f.hint for f in report.failures]}"
        )

    def test_student_syntax_error_gets_helpful_message(
        self, student_home, grade_calc_pack
    ):
        """Student has syntax error — gets clear feedback about what's wrong."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.runner import run_pack
        from ducky.workspace import start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        lesson_dir = start_lesson(pack_path)
        entry = lesson_dir / "grade_calculator.py"

        # Student has a syntax error
        entry.write_text(
            "def calculate_average grades:\n"  # Missing colon and parens
            "    return sum(grades) / len(grades)\n"
        )

        report = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)
        assert not report.passed

        # Should detect syntax error
        syntax_failures = [f for f in report.failures if f.diagnosis == "syntax_error"]
        assert len(syntax_failures) > 0

        # Hint should mention syntax
        for f in syntax_failures:
            assert "syntax" in f.hint.lower() or "colon" in f.hint.lower() or ":" in f.hint

    def test_student_empty_file_gets_all_missing(
        self, student_home, grade_calc_pack
    ):
        """Student submits completely empty file — all functions missing."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky.runner import run_pack
        from ducky.workspace import start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        lesson_dir = start_lesson(pack_path)
        entry = lesson_dir / "grade_calculator.py"

        # Empty file
        entry.write_text("")

        report = run_pack(str(pack_path), student_file=str(entry), timeout_seconds=15)
        assert not report.passed
        assert report.tests_failed > 0

        # All failures should be function_not_found
        for f in report.failures:
            assert f.diagnosis == "function_not_found"


# ════════════════════════════════════════════════════════════════════
# SECTION 8: Workspace Menu Simulation
# ════════════════════════════════════════════════════════════════════


class TestWorkspaceMenu:
    """Simulate the workspace menu flow (pack loading, testing)."""

    def test_workspace_load_pack(self, student_home, grade_calc_pack):
        """Student loads a pack into the workspace."""
        # HOME already set by student_home fixture via monkeypatch
        from ducky.pack import read_manifest

        manifest = read_manifest(grade_calc_pack)
        assert manifest.lesson_id == "grade_calculator"
        assert manifest.title == "Grade Calculator"
        assert manifest.entry_file == "grade_calculator.py"

    def test_workspace_run_tests_without_pack(self, student_home, capsys):
        """Running tests with no pack loaded shows an error (teacher grading lab)."""
        from ducky.gui.teacher import TeacherMenuMixin

        mixin = TeacherMenuMixin()
        mixin.current_pack = None
        with patch("builtins.input", return_value=""):
            mixin._workspace_run_tests()
        assert "No pack loaded" in capsys.readouterr().out

    def test_workspace_show_manifest(self, student_home, grade_calc_pack):
        """Student views pack manifest — sees lesson details."""
        from ducky.pack import read_manifest

        manifest = read_manifest(grade_calc_pack)
        data = manifest.to_dict()

        assert "lesson_id" in data
        assert "title" in data
        assert "entry_file" in data
        assert data["lesson_id"] == "grade_calculator"
        assert "functions" in data.get("concept_tags", [])


# ════════════════════════════════════════════════════════════════════
# SECTION 9: Student Client Communication
# ════════════════════════════════════════════════════════════════════


class TestStudentClient:
    """Test student↔teacher communication patterns."""

    def test_client_from_config(self, student_home):
        """StudentClient reads config correctly."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_config(
            student_home,
            teacher_url="http://localhost:8420",
            token="abc123",
            name="Maya",
        )

        from ducky.student_client import StudentClient

        client = StudentClient.from_config()
        assert client.teacher_url == "http://localhost:8420"
        assert client.token == "abc123"

    def test_client_no_teacher_configured(self, student_home):
        """StudentClient with no teacher URL — graceful handling."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_config(student_home, teacher_url="", token="", name="Maya")

        from ducky.student_client import StudentClient

        client = StudentClient.from_config()
        assert client.teacher_url == ""

    def test_client_connection_error(self, student_home):
        """StudentClient handles connection errors gracefully."""
        from ducky.student_client import StudentClient, StudentClientError

        client = StudentClient(teacher_url="http://localhost:99999")
        with pytest.raises(StudentClientError):
            client.connect("000000000", "Maya")


# ════════════════════════════════════════════════════════════════════
# SECTION 10: Progress Tracking
# ════════════════════════════════════════════════════════════════════


class TestProgressTracking:
    """Verify progress is tracked correctly through the journey."""

    def test_progress_starts_at_zero(self, student_home, monkeypatch):
        """Before any tests, progress is 0/0."""
        # HOME already set by student_home fixture via monkeypatch
        from ducky import workspace
        from ducky.workspace import get_progress

        # Mock to only count our packs (skip examples/packs fallback)
        monkeypatch.setattr(workspace, "list_available_lessons", lambda: [])

        progress = get_progress()
        assert progress["total"] == 0
        assert progress["completed"] == 0

    def test_progress_after_starting_lesson(
        self, student_home, grade_calc_pack, monkeypatch
    ):
        """After starting a lesson, progress shows 0 completed."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky import workspace
        from ducky.workspace import get_progress, start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        start_lesson(pack_path)

        # Mock to only count our lesson (skip examples/packs fallback)
        real_list = workspace.list_available_lessons
        def filtered_list():
            return [l for l in real_list() if l["lesson_id"] == "grade_calculator"]
        monkeypatch.setattr(workspace, "list_available_lessons", filtered_list)

        progress = get_progress()
        assert progress["total"] == 1
        assert progress["completed"] == 0
        assert progress["in_progress"] == 1

    def test_progress_after_completing_lesson(
        self, student_home, grade_calc_pack, monkeypatch
    ):
        """After passing all tests, progress shows 1 completed."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")

        from ducky import workspace
        from ducky.workspace import get_progress, mark_test_result, start_lesson

        pack_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        start_lesson(pack_path)
        mark_test_result("grade_calculator", True)

        # Mock to only count our lesson (skip examples/packs fallback)
        real_list = workspace.list_available_lessons
        def filtered_list():
            return [l for l in real_list() if l["lesson_id"] == "grade_calculator"]
        monkeypatch.setattr(workspace, "list_available_lessons", filtered_list)

        progress = get_progress()
        assert progress["total"] == 1
        assert progress["completed"] == 1

    def test_progress_with_multiple_lessons(
        self, student_home, grade_calc_pack, add_pack, monkeypatch
    ):
        """Progress tracks multiple lessons independently."""
        # HOME already set by student_home fixture via monkeypatch
        setup_student_with_pack(student_home, grade_calc_pack, "grade_calculator.duckypack")
        setup_student_with_pack(student_home, add_pack, "01_add_numbers.duckypack")

        from ducky import workspace
        from ducky.workspace import get_progress, mark_test_result, start_lesson

        # Start both
        gc_path = student_home / ".ducky" / "packs" / "grade_calculator.duckypack"
        add_path = student_home / ".ducky" / "packs" / "01_add_numbers.duckypack"
        start_lesson(gc_path)
        start_lesson(add_path)

        # Complete one
        mark_test_result("grade_calculator", True)

        # Mock to only count our lessons (skip examples/packs fallback)
        real_list = workspace.list_available_lessons
        def filtered_list():
            return [l for l in real_list() if l["lesson_id"] in ("grade_calculator", "01_add_numbers")]
        monkeypatch.setattr(workspace, "list_available_lessons", filtered_list)

        progress = get_progress()
        assert progress["total"] == 2
        assert progress["completed"] == 1
        assert progress["in_progress"] == 1
